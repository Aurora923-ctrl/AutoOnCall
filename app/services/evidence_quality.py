"""Shared evidence source quality rules for AIOps analysis and reports."""

from __future__ import annotations

from typing import Any

from app.services.diagnostic_signal_rules import infer_evidence_type, normalize_data_source

TRUSTED_EVIDENCE_SOURCES = {
    "prometheus",
    "loki",
    "log_gateway",
    "cmdb",
    "deploy_history",
    "redis_info",
    "kubernetes",
    "mysql",
    "ticket_api",
    "mcp_monitor",
    "mcp_cls",
    "eval_fixture",
}
FALLBACK_EVIDENCE_SOURCES = {
    "mock",
    "not_configured",
    "failed",
    "manual_analysis",
    "llm_toolnode_fallback",
    "rule_based",
}
DEGRADED_EVIDENCE_SOURCES = {"mcp_monitor_mixed", "unknown"}
NON_DIAGNOSTIC_EVIDENCE_TYPES = {"runbook", "risk"}
PRIMARY_DOMAIN_EVIDENCE_TYPES = {"redis", "mysql", "kubernetes", "k8s", "dependency"}
PRIMARY_DOMAIN_SOURCES = {"redis_info", "mysql", "kubernetes"}
SYMPTOM_EVIDENCE_TYPES = {"metric", "log"}
SYMPTOM_SOURCES = {"prometheus", "loki", "log_gateway"}
REFERENCE_EVIDENCE_TYPES = {"runbook", "ticket"}
REFERENCE_SOURCES = {"ticket_api"}

FALLBACK_ONLY_CONFIDENCE_CAP = 0.5
MIXED_WITH_FALLBACK_CONFIDENCE_CAP = 0.72


def build_evidence_quality_profile(
    evidence_items: list[Any],
    tool_records: list[Any] | None = None,
) -> dict[str, Any]:
    """Summarize evidence distribution and source quality."""
    by_type: dict[str, int] = {}
    by_stance: dict[str, int] = {}
    by_data_source: dict[str, int] = {}
    failed_tools = dedupe_strings(
        [
            str(record.get("tool_name") or "")
            for record in tool_records or []
            if isinstance(record, dict) and record.get("status") == "failed"
        ]
    )
    confidence_values: list[float] = []
    diagnostic_success_count = 0
    trusted_source_count = 0
    degraded_source_count = 0
    fallback_source_count = 0
    layer_counts = {"live": 0, "knowledge": 0, "history": 0, "other": 0}
    artifact_count = 0

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        evidence_type = evidence_type_for_quality(evidence)
        stance = str(evidence.get("stance") or "neutral")
        data_source = evidence_data_source(evidence)
        by_type[evidence_type] = by_type.get(evidence_type, 0) + 1
        by_stance[stance] = by_stance.get(stance, 0) + 1
        by_data_source[data_source] = by_data_source.get(data_source, 0) + 1
        layer = evidence_layer(evidence, evidence_type, data_source)
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        artifact_count += len(evidence_artifacts(evidence))
        confidence = evidence.get("confidence")
        if isinstance(confidence, int | float):
            confidence_values.append(float(confidence))
        raw_data = as_dict(evidence.get("raw_data"))
        if raw_data.get("status") == "failed":
            failed_tools.append(str(evidence.get("source_tool") or raw_data.get("tool_name") or ""))
        if is_successful_diagnostic_evidence(evidence, evidence_type):
            diagnostic_success_count += 1
            source_group = evidence_source_group(data_source)
            if source_group == "trusted":
                trusted_source_count += 1
            elif source_group == "fallback":
                fallback_source_count += 1
            else:
                degraded_source_count += 1

    source_quality = "trusted_or_unlabeled"
    low_quality_count = fallback_source_count + degraded_source_count
    if diagnostic_success_count and trusted_source_count == 0 and low_quality_count:
        source_quality = "fallback_only"
    elif low_quality_count:
        source_quality = "mixed_with_fallback"

    sufficiency = build_evidence_sufficiency(
        evidence_items,
        tool_records=tool_records,
        failed_tools=dedupe_strings([tool for tool in failed_tools if tool]),
    )
    root_cause_closure = build_root_cause_closure(evidence_items)

    return {
        "by_type": by_type,
        "by_stance": by_stance,
        "by_data_source": by_data_source,
        "by_layer": layer_counts,
        "failed_tools": dedupe_strings([tool for tool in failed_tools if tool]),
        "average_evidence_confidence": average(confidence_values),
        "diagnostic_success_count": diagnostic_success_count,
        "trusted_source_count": trusted_source_count,
        "degraded_source_count": degraded_source_count,
        "fallback_source_count": fallback_source_count,
        "artifact_count": artifact_count,
        "source_quality": source_quality,
        "root_cause_closure": root_cause_closure,
        "root_cause_closure_status": root_cause_closure["status"],
        "supporting_count": by_stance.get("supporting", 0),
        "refuting_count": by_stance.get("refuting", 0),
        "neutral_count": by_stance.get("neutral", 0),
        "unknown_count": by_stance.get("unknown", 0),
        "sufficiency": sufficiency,
        "sufficiency_status": sufficiency["status"],
        "confidence_cap": sufficiency["confidence_cap"],
    }


LIVE_EVIDENCE_SOURCES = {
    "prometheus",
    "loki",
    "log_gateway",
    "redis_info",
    "kubernetes",
    "mysql",
    "mcp_monitor",
    "mcp_cls",
}
LIVE_EVIDENCE_TYPES = {
    "metric",
    "log",
    "redis",
    "mysql",
    "k8s",
    "kubernetes",
    "trace",
    "message_queue",
    "alert",
}
KNOWLEDGE_EVIDENCE_TOOLS = {"search_runbook", "retrieve_runbook", "retrieve_knowledge"}
KNOWLEDGE_EVIDENCE_TYPES = {"runbook", "knowledge"}
HISTORY_EVIDENCE_SOURCES = {"ticket_api", "deploy_history"}
HISTORY_EVIDENCE_TOOLS = {"search_history_ticket", "query_deploy_history"}
HISTORY_EVIDENCE_TYPES = {"ticket", "change", "deploy_history"}


def build_root_cause_closure(evidence_items: list[Any]) -> dict[str, Any]:
    """Check whether root-cause claims have live evidence plus runbook/history support."""
    live_ids: list[str] = []
    knowledge_ids: list[str] = []
    history_ids: list[str] = []
    failed_critical_tools: list[str] = []

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        evidence_type = evidence_type_for_quality(evidence)
        data_source = evidence_data_source(evidence)
        source_tool = str(evidence.get("source_tool") or "")
        evidence_id = str(evidence.get("evidence_id") or source_tool or data_source)
        raw_data = as_dict(evidence.get("raw_data"))
        if raw_data.get("status") == "failed" and (
            evidence_type in PRIMARY_DOMAIN_EVIDENCE_TYPES
            or data_source in PRIMARY_DOMAIN_SOURCES
            or source_tool in {"query_redis_status", "query_mysql_status", "query_k8s_status"}
        ):
            failed_critical_tools.append(source_tool or data_source or evidence_type)
            continue
        if not is_successful_or_reference_evidence(evidence, evidence_type):
            continue
        if is_no_answer_reference(evidence):
            continue
        if str(evidence.get("stance") or "") not in {"supporting", "neutral"}:
            continue

        layer = evidence_layer(evidence, evidence_type, data_source)
        if layer == "live":
            live_ids.append(evidence_id)
        elif layer == "knowledge":
            knowledge_ids.append(evidence_id)
        elif layer == "history":
            history_ids.append(evidence_id)

    live_ids = dedupe_strings([item for item in live_ids if item])
    knowledge_ids = dedupe_strings([item for item in knowledge_ids if item])
    history_ids = dedupe_strings([item for item in history_ids if item])
    reference_ids = dedupe_strings([*knowledge_ids, *history_ids])
    missing: list[str] = []
    if not live_ids:
        missing.append("live evidence")
    if not reference_ids:
        missing.append("knowledge/history basis")
    status = "satisfied" if not missing else "incomplete"

    return {
        "status": status,
        "satisfied": status == "satisfied",
        "has_live_evidence": bool(live_ids),
        "has_knowledge_or_history": bool(reference_ids),
        "live_evidence_ids": live_ids,
        "knowledge_evidence_ids": knowledge_ids,
        "history_evidence_ids": history_ids,
        "missing": missing,
        "failed_critical_tools": dedupe_strings([item for item in failed_critical_tools if item]),
    }


def evidence_layer(evidence: dict[str, Any], evidence_type: str, data_source: str) -> str:
    source_tool = str(evidence.get("source_tool") or "").lower()
    if data_source in LIVE_EVIDENCE_SOURCES or evidence_type in LIVE_EVIDENCE_TYPES:
        return "live"
    if (
        data_source in HISTORY_EVIDENCE_SOURCES
        or source_tool in HISTORY_EVIDENCE_TOOLS
        or evidence_type in HISTORY_EVIDENCE_TYPES
    ):
        return "history"
    if source_tool in KNOWLEDGE_EVIDENCE_TOOLS or evidence_type in KNOWLEDGE_EVIDENCE_TYPES:
        return "knowledge"
    return "other"


def evidence_artifacts(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = evidence.get("artifact_refs")
    if isinstance(artifacts, list):
        return [item for item in artifacts if isinstance(item, dict)]
    raw_data = as_dict(evidence.get("raw_data"))
    output = as_dict(raw_data.get("output"))
    artifact = output.get("artifact_ref")
    if artifact:
        return [{"artifact_ref": artifact}]
    return []


def build_evidence_sufficiency(
    evidence_items: list[Any],
    *,
    tool_records: list[Any] | None = None,
    failed_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Return business-facing evidence sufficiency gates for report status decisions."""
    primary: list[str] = []
    symptom: list[str] = []
    reference: list[str] = []
    failed = list(failed_tools or [])
    failed_domain_types: set[str] = set()
    no_answer_reference_seen = False

    for evidence in evidence_items:
        if not isinstance(evidence, dict):
            continue
        evidence_type = evidence_type_for_quality(evidence)
        data_source = evidence_data_source(evidence)
        source_tool = str(evidence.get("source_tool") or "")
        raw_data = as_dict(evidence.get("raw_data"))
        status = str(raw_data.get("status") or "")

        if status == "failed":
            failed.append(source_tool or data_source)
            if (
                evidence_type in PRIMARY_DOMAIN_EVIDENCE_TYPES
                or data_source in PRIMARY_DOMAIN_SOURCES
            ):
                failed_domain_types.add(evidence_type)
            continue
        if is_no_answer_reference(evidence):
            no_answer_reference_seen = True
            continue
        if not is_successful_or_reference_evidence(evidence, evidence_type):
            continue
        marker = source_tool or data_source or evidence_type
        if (
            evidence_type in PRIMARY_DOMAIN_EVIDENCE_TYPES
            or data_source in PRIMARY_DOMAIN_SOURCES
            or is_resource_domain_metric(evidence, evidence_type)
        ):
            primary.append(marker)
        if evidence_type in SYMPTOM_EVIDENCE_TYPES or data_source in SYMPTOM_SOURCES:
            symptom.append(marker)
        if (
            evidence_type in REFERENCE_EVIDENCE_TYPES
            or data_source in REFERENCE_SOURCES
            or source_tool in {"search_runbook", "retrieve_knowledge", "search_history_ticket"}
        ):
            reference.append(marker)

    for record in tool_records or []:
        if not isinstance(record, dict):
            continue
        if record.get("status") == "failed":
            failed.append(str(record.get("tool_name") or ""))

    primary = dedupe_strings([item for item in primary if item])
    symptom = dedupe_strings([item for item in symptom if item])
    reference = dedupe_strings([item for item in reference if item])
    failed = dedupe_strings([item for item in failed if item])
    missing: list[str] = []
    if failed_domain_types:
        primary = [
            item for item in primary if item not in {"query_metrics", "prometheus", "metric"}
        ]
    if not primary:
        missing.append("主故障域工具证据（Redis / MySQL / K8s）")
    if not symptom:
        missing.append("现象侧证据（metrics 或 logs）")
    if not reference:
        missing.append("处置参考（Runbook 或历史工单）")
    if no_answer_reference_seen:
        missing = [item for item in missing if item != "处置参考（Runbook 或历史工单）"]
        missing.append("可信 Runbook / 历史工单处置参考")

    complete = not missing
    if complete:
        status = "complete"
        confidence_cap = None
    elif primary and symptom:
        status = "degraded"
        confidence_cap = 0.74
    else:
        status = "incomplete"
        confidence_cap = 0.55

    return {
        "complete": complete,
        "status": status,
        "has_primary_domain_evidence": bool(primary),
        "has_symptom_evidence": bool(symptom),
        "has_reference_evidence": bool(reference),
        "primary_domain_tools": primary,
        "symptom_tools": symptom,
        "reference_tools": reference,
        "missing_evidence": missing,
        "failed_tools": failed,
        "confidence_cap": confidence_cap,
    }


def source_quality_confidence_cap(
    evidence: list[dict[str, Any]],
    evidence_analysis: dict[str, Any],
) -> float | None:
    """Return the confidence cap implied by evidence source quality."""
    profile = as_dict(evidence_analysis.get("evidence_profile"))
    source_quality = str(profile.get("source_quality") or "")
    if source_quality == "fallback_only":
        return FALLBACK_ONLY_CONFIDENCE_CAP
    if source_quality == "mixed_with_fallback":
        return MIXED_WITH_FALLBACK_CONFIDENCE_CAP

    derived_profile = build_evidence_quality_profile(evidence)
    derived_quality = str(derived_profile.get("source_quality") or "")
    if derived_quality == "fallback_only":
        return FALLBACK_ONLY_CONFIDENCE_CAP
    if derived_quality == "mixed_with_fallback":
        return MIXED_WITH_FALLBACK_CONFIDENCE_CAP
    return None


def evidence_data_source(evidence: dict[str, Any]) -> str:
    """Return a normalized data source label for one evidence item."""
    data_source = str(evidence.get("data_source") or "").strip().lower()
    if data_source and data_source != "unknown":
        return data_source
    raw_data = as_dict(evidence.get("raw_data"))
    normalized = normalize_data_source(str(evidence.get("source_tool") or ""), raw_data)
    if normalized and normalized != "unknown":
        return normalized
    return "unknown"


def evidence_source_group(data_source: str) -> str:
    """Classify a normalized source as trusted, fallback, or degraded."""
    source = str(data_source or "").strip().lower()
    if source in TRUSTED_EVIDENCE_SOURCES:
        return "trusted"
    if source in FALLBACK_EVIDENCE_SOURCES:
        return "fallback"
    return "degraded"


def evidence_type_for_quality(evidence: dict[str, Any]) -> str:
    evidence_type = str(evidence.get("evidence_type") or "")
    if evidence_type:
        return evidence_type
    return infer_evidence_type(str(evidence.get("source_tool") or ""))


def is_successful_diagnostic_evidence(evidence: dict[str, Any], evidence_type: str) -> bool:
    if evidence_type in NON_DIAGNOSTIC_EVIDENCE_TYPES:
        return False
    return _evidence_is_usable(evidence)


def is_successful_or_reference_evidence(evidence: dict[str, Any], evidence_type: str) -> bool:
    raw_data = as_dict(evidence.get("raw_data"))
    if raw_data.get("status") == "success" and _evidence_is_usable(evidence):
        return True
    return False


def _evidence_is_usable(evidence: dict[str, Any]) -> bool:
    raw_data = as_dict(evidence.get("raw_data"))
    if raw_data.get("status") != "success":
        return False
    output = raw_data.get("output")
    if not _has_meaningful_output(output):
        return False
    if is_no_answer_reference(evidence):
        return False
    metadata = as_dict(raw_data.get("metadata"))
    quality = as_dict(metadata.get("evidence_quality"))
    return quality.get("usable", True) is not False


def _has_meaningful_output(output: Any) -> bool:
    """Reject successful envelopes that contain no diagnostic observation."""
    if output is None:
        return False
    if isinstance(output, str):
        return output.strip().lower() not in {"", "ok", "success", "empty", "no data"}
    if isinstance(output, (list, tuple, set)):
        return bool(output)
    if not isinstance(output, dict):
        return True
    meaningful_keys = {
        key
        for key, value in output.items()
        if key not in {"status", "source", "metadata", "error_type", "no_answer_rejected"}
        and value not in (None, "", [], {}, ())
    }
    if not meaningful_keys:
        return any(
            output.get(key) not in (None, "", [], {}, ())
            for key in ("source", "summary", "logs", "signals", "data")
        )
    summary = str(output.get("summary") or "").strip().lower()
    return bool(summary) or bool(meaningful_keys)


def is_resource_domain_metric(evidence: dict[str, Any], evidence_type: str) -> bool:
    """Treat CPU/memory/disk metric evidence as the primary domain for resource incidents."""
    if evidence_type != "metric":
        return False
    text = " ".join(
        [
            str(evidence.get("summary") or ""),
            str(evidence.get("fact") or ""),
            str(evidence.get("inference") or ""),
            str(as_dict(evidence.get("raw_data")).get("output") or ""),
        ]
    ).lower()
    return any(
        token in text
        for token in [
            "cpu",
            "memory",
            "oom",
            "disk",
            "no space",
            "磁盘",
            "内存",
        ]
    )


def is_no_answer_reference(evidence: dict[str, Any]) -> bool:
    """Return True when retrieval ran but explicitly rejected all references."""
    raw_data = as_dict(evidence.get("raw_data"))
    output = as_dict(raw_data.get("output"))
    return bool(raw_data.get("no_answer_rejected") or output.get("no_answer_rejected"))


def evidence_data_sources(evidence_items: list[Any]) -> set[str]:
    sources: set[str] = set()
    for evidence in evidence_items:
        if isinstance(evidence, dict):
            sources.add(evidence_data_source(evidence))
    return sources


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
